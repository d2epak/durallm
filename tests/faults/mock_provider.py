"""Programmable Mock Provider for Deterministic Fault Injection."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from durallm.agent.context import estimate_tokens
from durallm.capability.profile import Endpoint
from durallm.protocol.ir import (
    NormalizedRequest,
    NormalizedResponse,
)
from durallm.providers.base import (
    PreparedRequest,
    ProviderExecutionResult,
)


@dataclass
class MockFaultAction:
    """A configured fault or response action for a mock provider."""
    status_code: int = 200
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b"{}"
    delay_ms: float = 0.0
    side_effect: Optional[Callable[[], None]] = None

    @classmethod
    def success(cls, content: str = "Success", tool_calls: Optional[List[Dict[str, Any]]] = None) -> MockFaultAction:
        message: Dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = tool_calls
        payload = {
            "id": "mock_success_1",
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 25},
        }
        return cls(status_code=200, body=json.dumps(payload).encode("utf-8"))

    @classmethod
    def rate_limit(cls, retry_after: int = 5) -> MockFaultAction:
        body = json.dumps({"error": {"message": "Rate limit reached", "code": 429}}).encode("utf-8")
        return cls(status_code=429, headers={"retry-after": str(retry_after)}, body=body)

    @classmethod
    def server_error(cls, status_code: int = 500, message: str = "Internal server error") -> MockFaultAction:
        body = json.dumps({"error": {"message": message, "code": status_code}}).encode("utf-8")
        return cls(status_code=status_code, body=body)

    @classmethod
    def timeout(cls, duration_ms: float = 30000.0) -> MockFaultAction:
        return cls(status_code=504, delay_ms=duration_ms, body=b'{"error": {"message": "Gateway Timeout"}}')

    @classmethod
    def context_overflow(cls) -> MockFaultAction:
        body = json.dumps({"error": {"message": "context_length_exceeded: maximum context length is 32768 tokens", "code": 400}}).encode("utf-8")
        return cls(status_code=400, body=body)

    @classmethod
    def malformed_tool_call(cls, tool_name: str = "bash") -> MockFaultAction:
        # Invalid arguments (unparseable syntax or missing required keys)
        return cls.malformed_tool_json("{invalid json: true,", tool_name=tool_name)

    @classmethod
    def malformed_tool_json(cls, raw_arguments: str, tool_name: str = "bash") -> MockFaultAction:
        message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "tc_malformed",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": raw_arguments},
                }
            ],
        }
        payload = {
            "id": "mock_tool_fail",
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
        }
        return cls(status_code=200, body=json.dumps(payload).encode("utf-8"))

    @classmethod
    def valid_tool_call(cls, tool_name: str, arguments: Dict[str, Any]) -> MockFaultAction:
        message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"tc_{tool_name}",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": json.dumps(arguments)},
                }
            ],
        }
        payload = {
            "id": "mock_tool_success",
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
        }
        return cls(status_code=200, body=json.dumps(payload).encode("utf-8"))

    @classmethod
    def mid_stream_reset(cls, partial_content: str = "") -> MockFaultAction:
        return cls(status_code=502, body=b'{"error": {"message": "Connection reset mid-stream"}}')


class ProgrammableMockAdapter:
    """Mock Provider Adapter whose behavior is programmed via a sequence of MockFaultActions."""

    def __init__(
        self,
        provider_id: str,
        call_log: Optional[List[str]] = None,
        context_window: Optional[int] = None,
    ):
        self.provider_id = provider_id
        # When set, a request whose estimated input tokens exceed the window is answered with
        # 400 context_length_exceeded (like a real provider) without consuming a scripted action.
        self.context_window = context_window
        self.actions: List[MockFaultAction] = []
        self.current_index: int = 0
        self.call_history: List[PreparedRequest] = []
        # The IR request behind each prepared request, so tests can inspect what the provider saw.
        self.request_history: List[NormalizedRequest] = []
        # Optional log shared between adapters: provider_id appended per call, in global order.
        self.call_log: Optional[List[str]] = call_log
        self._default_action = MockFaultAction.success()

    def set_sequence(self, actions: List[MockFaultAction]) -> None:
        """Program the sequence of responses for subsequent calls."""
        self.actions = list(actions)
        self.current_index = 0

    def prepare_request(
        self,
        endpoint: Endpoint,
        request: NormalizedRequest,
        api_key: Optional[str] = None,
    ) -> PreparedRequest:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.request_history.append(request)
        return PreparedRequest(
            url=f"mock://{endpoint.provider}/{endpoint.model}",
            headers=headers,
            body_bytes=json.dumps({"estimated_tokens": estimate_tokens(request)}).encode("utf-8"),
        )

    def execute(
        self,
        prepared: PreparedRequest,
        timeout_seconds: float,
    ) -> ProviderExecutionResult:
        self.call_history.append(prepared)
        if self.call_log is not None:
            self.call_log.append(self.provider_id)

        if self.context_window is not None:
            sent_tokens = json.loads(prepared.body_bytes.decode("utf-8")).get("estimated_tokens", 0)
            if sent_tokens > self.context_window:
                overflow = MockFaultAction.context_overflow()
                return ProviderExecutionResult(status_code=overflow.status_code, headers={}, body=overflow.body, duration_ms=1.0)

        if self.current_index < len(self.actions):
            action = self.actions[self.current_index]
            self.current_index += 1
        else:
            action = self._default_action

        if action.side_effect:
            action.side_effect()

        delay = min(action.delay_ms, timeout_seconds * 1000.0) if action.delay_ms > 0 else 10.0

        if action.delay_ms >= timeout_seconds * 1000.0:
            # Simulate real timeout breach
            return ProviderExecutionResult(
                status_code=504,
                headers={},
                body=b'{"error": {"message": "Timed out"}}',
                duration_ms=timeout_seconds * 1000.0,
            )

        return ProviderExecutionResult(
            status_code=action.status_code,
            headers=action.headers,
            body=action.body,
            duration_ms=delay,
        )

    def normalize_response(
        self,
        endpoint: Endpoint,
        result: ProviderExecutionResult,
    ) -> NormalizedResponse:
        from durallm.protocol.openai import openai_response_to_ir
        raw_json = json.loads(result.body.decode("utf-8"))
        return openai_response_to_ir(raw_json)
