"""Protocol Intermediate Representation (IR) and Adapters."""

from durallm.protocol.anthropic import (
    anthropic_request_to_ir,
    ir_to_anthropic_request,
    ir_to_anthropic_response,
)
from durallm.protocol.gemini import (
    gemini_response_to_ir,
    ir_to_gemini_request,
)
from durallm.protocol.ir import (
    NormalizedMessage,
    NormalizedRequest,
    NormalizedResponse,
    NormalizedToolCall,
    NormalizedToolDefinition,
    NormalizedToolResult,
)
from durallm.protocol.openai import (
    ir_to_openai_request,
    ir_to_openai_response,
    openai_request_to_ir,
    openai_response_to_ir,
)

__all__ = [
    "NormalizedRequest",
    "NormalizedResponse",
    "NormalizedMessage",
    "NormalizedToolCall",
    "NormalizedToolResult",
    "NormalizedToolDefinition",
    "anthropic_request_to_ir",
    "ir_to_anthropic_request",
    "ir_to_anthropic_response",
    "openai_request_to_ir",
    "ir_to_openai_request",
    "openai_response_to_ir",
    "ir_to_openai_response",
    "ir_to_gemini_request",
    "gemini_response_to_ir",
]
