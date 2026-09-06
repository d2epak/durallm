"""Canonical Protocol Intermediate Representation (IR).

Prevents N² translation complexity by defining a single neutral representation
for requests, responses, tools, messages, and structured output across all providers.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class NormalizedToolDefinition:
    """Neutral tool definition."""
    name: str
    description: str = ""
    parameters: Dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})


@dataclass
class NormalizedToolCall:
    """Neutral tool invocation made by an assistant."""
    id: str
    name: str
    arguments: Dict[str, Any]
    raw_arguments: str = ""
    # Gateway annotations for the client: ledger_call_id, replayed, execution_receipt.
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.raw_arguments and self.arguments:
            self.raw_arguments = json.dumps(self.arguments, ensure_ascii=False)


@dataclass
class NormalizedToolResult:
    """Result of a tool execution fed back to the model."""
    tool_call_id: str
    tool_name: Optional[str] = None
    content: str = ""
    is_error: bool = False


@dataclass
class NormalizedMessage:
    """Neutral conversation turn."""
    role: str  # "system", "user", "assistant", "tool"
    content: str = ""
    reasoning_content: Optional[str] = None
    # Anthropic thinking-block signature; thinking is only re-emitted to Anthropic when present.
    reasoning_signature: Optional[str] = None
    tool_calls: List[NormalizedToolCall] = field(default_factory=list)
    tool_results: List[NormalizedToolResult] = field(default_factory=list)
    name: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedRequest:
    """Neutral model invocation request."""
    request_id: str = field(default_factory=lambda: f"req_{uuid.uuid4().hex[:12]}")
    model: str = "default"
    messages: List[NormalizedMessage] = field(default_factory=list)
    system_instruction: Optional[str] = None
    tools: List[NormalizedToolDefinition] = field(default_factory=list)
    tool_choice: Optional[Any] = None
    max_output_tokens: Optional[int] = None
    temperature: Optional[float] = None
    stream: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def get_effective_system_instruction(self) -> Optional[str]:
        """Return explicit system instruction or extracted leading system turn."""
        if self.system_instruction:
            return self.system_instruction
        for m in self.messages:
            if m.role == "system":
                return m.content
        return None


@dataclass
class NormalizedResponse:
    """Neutral model completion response."""
    response_id: str = field(default_factory=lambda: f"resp_{uuid.uuid4().hex[:12]}")
    model: str = "default"
    content: Optional[str] = None
    reasoning_content: Optional[str] = None
    reasoning_signature: Optional[str] = None
    tool_calls: List[NormalizedToolCall] = field(default_factory=list)
    finish_reason: str = "stop"  # "stop", "tool_calls", "length", "content_filter", "error"
    input_tokens: int = 0
    output_tokens: int = 0
    raw_response: Optional[Dict[str, Any]] = None
