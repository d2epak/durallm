"""Tool Call Schema Validation and Deterministic Syntactic Repair."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class ToolCallResult(str, Enum):
    VALID = "valid"
    NORMALIZED = "normalized"
    INVALID = "invalid"
    UNSAFE_TO_REPAIR = "unsafe_to_repair"


@dataclass
class ToolValidationReport:
    """Detailed audit report for a tool validation attempt."""
    tool_name: str
    status: ToolCallResult
    validated_arguments: Dict[str, Any]
    raw_arguments: str
    normalizations_applied: List[str] = field(default_factory=list)
    error_message: Optional[str] = None

    @property
    def is_executable(self) -> bool:
        """Only valid or safely normalized tool calls are safe to execute."""
        return self.status in (ToolCallResult.VALID, ToolCallResult.NORMALIZED)


class ToolCallValidator:
    """
    Validates tool invocations against tool schema definitions.
    Enforces Rule 3: Never invent missing arguments, never guess tool names,
    allow deterministic syntactic normalization, fail closed on semantic uncertainty.
    """

    def __init__(
        self,
        strict: bool = True,
        allow_syntactic_repairs: bool = True,
        allow_semantic_repairs: bool = False,
    ):
        self.strict = strict
        self.allow_syntactic_repairs = allow_syntactic_repairs
        self.allow_semantic_repairs = allow_semantic_repairs  # Default False (semantic guessing forbidden)

    def normalize_json_syntax(self, raw: str) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        """
        Perform safe, deterministic syntactic normalization on JSON strings:
        - strip markdown code fences (```json ... ```)
        - strip trailing commas before closing braces/brackets
        - strip leading/trailing whitespace
        """
        normalizations: List[str] = []
        if not raw or not raw.strip():
            return {}, ["empty_to_dict"]

        cleaned = raw.strip()
        # 1. Remove markdown backticks
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
            normalizations.append("strip_markdown_fences")

        # 2. Strip trailing commas
        re_comma = re.sub(r",\s*([\]}])", r"\1", cleaned)
        if re_comma != cleaned:
            cleaned = re_comma
            normalizations.append("strip_trailing_commas")

        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed, normalizations
            return None, ["non_dict_json"]
        except Exception:
            return None, ["unparseable_json"]

    def validate_tool_call(
        self,
        tool_name: str,
        arguments: Any,
        schema: Optional[Dict[str, Any]] = None,
        known_tools: Optional[List[str]] = None,
    ) -> ToolValidationReport:
        """Validate a tool call against the tool's parameter schema."""
        raw_str = arguments if isinstance(arguments, str) else json.dumps(arguments or {}, ensure_ascii=False)

        # 1. Unknown tool check
        if known_tools is not None and tool_name not in known_tools:
            return ToolValidationReport(
                tool_name=tool_name,
                status=ToolCallResult.INVALID,
                validated_arguments={},
                raw_arguments=raw_str,
                error_message=f"Tool '{tool_name}' is not in active tool catalog {known_tools}",
            )

        # 2. Argument Parsing & Syntactic Normalization
        normalizations: List[str] = []
        parsed_args: Optional[Dict[str, Any]] = None

        if isinstance(arguments, dict):
            parsed_args = dict(arguments)
        elif isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
                if isinstance(parsed, dict):
                    parsed_args = parsed
            except Exception:
                if self.allow_syntactic_repairs:
                    parsed_args, normalizations = self.normalize_json_syntax(arguments)

        if parsed_args is None:
            # Semantic repair / guessing is strictly prohibited in strict mode
            if self.strict or not self.allow_semantic_repairs:
                return ToolValidationReport(
                    tool_name=tool_name,
                    status=ToolCallResult.UNSAFE_TO_REPAIR,
                    validated_arguments={},
                    raw_arguments=raw_str,
                    error_message="Arguments cannot be parsed into valid JSON and semantic guessing is disabled",
                )
            # In non-strict mode with explicit semantic repairs allowed:
            return ToolValidationReport(
                tool_name=tool_name,
                status=ToolCallResult.INVALID,
                validated_arguments={},
                raw_arguments=raw_str,
                error_message="Malformed arguments",
            )

        # 3. Schema Property & Type Validation
        if schema:
            schema_params = schema.get("parameters") or schema
            properties = schema_params.get("properties", {})
            required_keys = schema_params.get("required", [])

            # Check missing required fields
            missing = [k for k in required_keys if k not in parsed_args]
            if missing:
                # Rule 3: NEVER invent missing required properties!
                return ToolValidationReport(
                    tool_name=tool_name,
                    status=ToolCallResult.INVALID,
                    validated_arguments=parsed_args,
                    raw_arguments=raw_str,
                    normalizations_applied=normalizations,
                    error_message=f"Missing required argument(s): {missing}",
                )

            # Check additionalProperties
            if schema_params.get("additionalProperties") is False:
                extra = [k for k in parsed_args.keys() if k not in properties]
                if extra:
                    return ToolValidationReport(
                        tool_name=tool_name,
                        status=ToolCallResult.INVALID,
                        validated_arguments=parsed_args,
                        raw_arguments=raw_str,
                        normalizations_applied=normalizations,
                        error_message=f"Disallowed additional argument(s): {extra}",
                    )

            # Check types
            for prop_name, prop_spec in properties.items():
                if prop_name not in parsed_args:
                    continue
                val = parsed_args[prop_name]
                expected_type = prop_spec.get("type")

                if expected_type == "integer" and not isinstance(val, int):
                    # Deterministic safe coercion if numeric string
                    if isinstance(val, str) and val.isdigit() and self.allow_syntactic_repairs:
                        parsed_args[prop_name] = int(val)
                        normalizations.append(f"coerce_{prop_name}_str_to_int")
                    else:
                        return ToolValidationReport(
                            tool_name=tool_name,
                            status=ToolCallResult.INVALID,
                            validated_arguments=parsed_args,
                            raw_arguments=raw_str,
                            normalizations_applied=normalizations,
                            error_message=f"Property '{prop_name}' expected integer, got {type(val).__name__}",
                        )

                elif expected_type == "string" and not isinstance(val, str):
                    if isinstance(val, (int, float, bool)) and self.allow_syntactic_repairs:
                        parsed_args[prop_name] = str(val)
                        normalizations.append(f"coerce_{prop_name}_to_str")
                    else:
                        return ToolValidationReport(
                            tool_name=tool_name,
                            status=ToolCallResult.INVALID,
                            validated_arguments=parsed_args,
                            raw_arguments=raw_str,
                            normalizations_applied=normalizations,
                            error_message=f"Property '{prop_name}' expected string, got {type(val).__name__}",
                        )

                elif expected_type == "array" and not isinstance(val, list):
                    return ToolValidationReport(
                        tool_name=tool_name,
                        status=ToolCallResult.INVALID,
                        validated_arguments=parsed_args,
                        raw_arguments=raw_str,
                        normalizations_applied=normalizations,
                        error_message=f"Property '{prop_name}' expected array, got {type(val).__name__}",
                    )

        status = ToolCallResult.NORMALIZED if normalizations else ToolCallResult.VALID
        return ToolValidationReport(
            tool_name=tool_name,
            status=status,
            validated_arguments=parsed_args,
            raw_arguments=raw_str,
            normalizations_applied=normalizations,
        )
