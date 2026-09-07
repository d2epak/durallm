"""Tool Call Schema Validation and Deterministic Syntactic Repair."""

from __future__ import annotations

import ast
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
    Enforces Iron Rules:
    - Rule 1: Fail closed on missing required arguments (never invent or guess).
    - Rule 2: Safe syntactic normalization (fences, commas, unescaped newlines, envelopes),
      strictly prohibiting semantic alterations.
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
        - strip markdown code fences (```json ... ```, ``` ... ```)
        - strip trailing commas before closing braces/brackets
        - allow unescaped newlines in code strings via strict=False
        - convert single-quoted JSON-like dictionaries safely via AST
        """
        normalizations: List[str] = []
        if not raw or not raw.strip():
            return {}, ["empty_to_dict"]

        cleaned = raw.strip()
        # 1. Remove markdown backticks
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:[a-zA-Z0-9_\-]+)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
            normalizations.append("strip_markdown_fences")

        # 2. Strip trailing commas
        re_comma = re.sub(r",\s*([\]}])", r"\1", cleaned)
        if re_comma != cleaned:
            cleaned = re_comma
            normalizations.append("strip_trailing_commas")

        # 3. Standard JSON parse with strict=False (allows raw newlines/tabs in string literals)
        try:
            parsed = json.loads(cleaned, strict=False)
            if isinstance(parsed, dict):
                return parsed, normalizations
            return None, ["non_dict_json"]
        except Exception:
            pass

        # 4. Safe Python-syntax dictionary parse (for single-quoted JSON dicts)
        try:
            parsed_ast = ast.literal_eval(cleaned)
            if isinstance(parsed_ast, dict):
                normalizations.append("python_dict_to_json")
                return parsed_ast, normalizations
        except Exception:
            pass

        return None, ["unparseable_json"]

    def _validate_schema(
        self,
        target_dict: Dict[str, Any],
        schema_params: Dict[str, Any],
        normalizations: List[str],
        path_prefix: str = "",
    ) -> Optional[str]:
        """Recursively validate parameters against JSON Schema (Iron Rule 1 & 2)."""
        properties = schema_params.get("properties", {})
        required_keys = schema_params.get("required", [])

        # Check missing required fields (Iron Rule 1: Fail closed)
        missing = [k for k in required_keys if k not in target_dict]
        if missing:
            field_desc = f" at '{path_prefix}'" if path_prefix else ""
            return f"Missing required argument(s){field_desc}: {missing}"

        # Check additionalProperties
        if schema_params.get("additionalProperties") is False:
            extra = [k for k in target_dict.keys() if k not in properties]
            if extra:
                field_desc = f" at '{path_prefix}'" if path_prefix else ""
                return f"Disallowed additional argument(s){field_desc}: {extra}"

        # Check properties and nested types
        for prop_name, prop_spec in properties.items():
            if prop_name not in target_dict:
                continue
            val = target_dict[prop_name]
            expected_type = prop_spec.get("type")
            field_name = f"{path_prefix}.{prop_name}" if path_prefix else prop_name

            if expected_type == "integer" and not isinstance(val, int):
                if isinstance(val, str) and val.isdigit() and self.allow_syntactic_repairs:
                    target_dict[prop_name] = int(val)
                    normalizations.append(f"coerce_{field_name}_str_to_int")
                else:
                    return f"Property '{field_name}' expected integer, got {type(val).__name__}"

            elif expected_type in ("number", "float") and not isinstance(val, (int, float)):
                if isinstance(val, str) and self.allow_syntactic_repairs:
                    try:
                        target_dict[prop_name] = float(val)
                        normalizations.append(f"coerce_{field_name}_str_to_number")
                    except ValueError:
                        return f"Property '{field_name}' expected number, got string '{val}'"
                else:
                    return f"Property '{field_name}' expected number, got {type(val).__name__}"

            elif expected_type == "boolean" and not isinstance(val, bool):
                if isinstance(val, str) and val.lower() in ("true", "false") and self.allow_syntactic_repairs:
                    target_dict[prop_name] = (val.lower() == "true")
                    normalizations.append(f"coerce_{field_name}_str_to_bool")
                else:
                    return f"Property '{field_name}' expected boolean, got {type(val).__name__}"

            elif expected_type == "string" and not isinstance(val, str):
                if isinstance(val, (int, float, bool)) and self.allow_syntactic_repairs:
                    target_dict[prop_name] = str(val)
                    normalizations.append(f"coerce_{field_name}_to_str")
                else:
                    return f"Property '{field_name}' expected string, got {type(val).__name__}"

            elif expected_type == "array" and not isinstance(val, list):
                return f"Property '{field_name}' expected array, got {type(val).__name__}"

            elif expected_type == "array" and isinstance(val, list):
                item_spec = prop_spec.get("items")
                if isinstance(item_spec, dict) and item_spec.get("type") == "object":
                    for idx, item in enumerate(val):
                        if not isinstance(item, dict):
                            return f"Item {idx} in array '{field_name}' expected object, got {type(item).__name__}"
                        err = self._validate_schema(item, item_spec, normalizations, path_prefix=f"{field_name}[{idx}]")
                        if err:
                            return err

            elif expected_type == "object" and not isinstance(val, dict):
                return f"Property '{field_name}' expected object, got {type(val).__name__}"

            elif expected_type == "object" and isinstance(val, dict):
                if prop_spec.get("properties") or prop_spec.get("required"):
                    err = self._validate_schema(val, prop_spec, normalizations, path_prefix=field_name)
                    if err:
                        return err

        return None

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
                parsed = json.loads(arguments, strict=False)
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
            return ToolValidationReport(
                tool_name=tool_name,
                status=ToolCallResult.INVALID,
                validated_arguments={},
                raw_arguments=raw_str,
                error_message="Malformed arguments",
            )

        # 3. Envelope Unwrapping if needed
        schema_params = (schema.get("parameters") or schema) if schema else None
        if schema_params and self.allow_syntactic_repairs:
            expected_props = schema_params.get("properties", {})
            for wrapper in ("parameters", "arguments", "input"):
                if wrapper in parsed_args and len(parsed_args) == 1 and isinstance(parsed_args[wrapper], dict):
                    if wrapper not in expected_props:
                        parsed_args = dict(parsed_args[wrapper])
                        normalizations.append(f"unwrap_{wrapper}_envelope")
                        break

        # 4. Schema Property & Type Validation (Fail Closed)
        if schema_params:
            err = self._validate_schema(parsed_args, schema_params, normalizations)
            if err:
                return ToolValidationReport(
                    tool_name=tool_name,
                    status=ToolCallResult.INVALID,
                    validated_arguments=parsed_args,
                    raw_arguments=raw_str,
                    normalizations_applied=normalizations,
                    error_message=err,
                )

        status = ToolCallResult.NORMALIZED if normalizations else ToolCallResult.VALID
        return ToolValidationReport(
            tool_name=tool_name,
            status=status,
            validated_arguments=parsed_args,
            raw_arguments=raw_str,
            normalizations_applied=normalizations,
        )

