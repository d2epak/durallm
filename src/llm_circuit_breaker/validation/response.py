"""Response Validation and Semantic Sanity Checks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from llm_circuit_breaker.agent.tool_validation import ToolCallValidator
from llm_circuit_breaker.protocol.ir import NormalizedRequest, NormalizedResponse

logger = logging.getLogger("llm_circuit_breaker.validation")


@dataclass
class ResponseValidationResult:
    """Outcome of response validation pipeline."""
    is_valid: bool
    error_message: Optional[str] = None
    rejection_reason: Optional[str] = None
    sanitized_response: Optional[NormalizedResponse] = None
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0


class ResponseValidator:
    """
    Validates model responses beyond HTTP status codes:
    decode -> normalize -> sanity checks -> size checks -> tool validation -> usage extraction.
    HTTP 200 does NOT equal semantic success.
    """

    def __init__(
        self,
        tool_validator: Optional[ToolCallValidator] = None,
        max_response_chars: int = 5_000_000,
        allow_empty_content_with_tools: bool = True,
    ):
        self.tool_validator = tool_validator or ToolCallValidator(strict=True)
        self.max_response_chars = max_response_chars
        self.allow_empty_content_with_tools = allow_empty_content_with_tools

    def check_sanity(self, response: NormalizedResponse) -> Optional[ResponseValidationResult]:
        """Body-level checks that do not need the request: returns a rejection result, or None if sane."""
        # 1. Sanity: Response must have at least content or tool calls
        has_content = bool(response.content and response.content.strip())
        has_tools = bool(response.tool_calls)

        if not has_content and not has_tools:
            return ResponseValidationResult(
                is_valid=False,
                error_message="Model returned HTTP 200 with completely empty content and no tool calls",
                rejection_reason="empty_response",
            )

        # 2. Response Size Bomb Defense
        if response.content and len(response.content) > self.max_response_chars:
            return ResponseValidationResult(
                is_valid=False,
                error_message=f"Response exceeded maximum character limit ({len(response.content)} > {self.max_response_chars})",
                rejection_reason="response_size_exhaustion",
            )
        return None

    def validate(
        self,
        response: NormalizedResponse,
        request: NormalizedRequest,
    ) -> ResponseValidationResult:
        sanity_failure = self.check_sanity(response)
        if sanity_failure is not None:
            return sanity_failure

        # 3. Tool Schema Validation
        known_tools = [t.name for t in request.tools] if request.tools else []
        for tc in response.tool_calls:
            tool_schema = next((t.parameters for t in (request.tools or []) if t.name == tc.name), None)
            report = self.tool_validator.validate_tool_call(
                tool_name=tc.name,
                arguments=tc.arguments,
                schema=tool_schema,
                known_tools=known_tools,
            )
            if not report.is_executable:
                return ResponseValidationResult(
                    is_valid=False,
                    error_message=f"Invalid tool call '{tc.name}': {report.error_message}",
                    rejection_reason="malformed_tool_call",
                )
            tc.arguments = report.validated_arguments

        # 4. Token usage (NormalizedResponse carries counts directly; there is no raw `usage` dict)
        in_tokens = response.input_tokens or 0
        out_tokens = response.output_tokens or 0

        return ResponseValidationResult(
            is_valid=True,
            sanitized_response=response,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
        )


DEFAULT_RESPONSE_VALIDATOR = ResponseValidator()
