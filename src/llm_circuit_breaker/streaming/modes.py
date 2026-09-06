"""Streaming Modes and Mid-Stream Failure Policies."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterator

from llm_circuit_breaker.protocol.ir import NormalizedResponse


class StreamingMode(str, Enum):
    """Explicit streaming modes."""
    TRUE_STREAMING = "true_streaming"  # Mode A: Direct chunk passthrough
    ATOMIC_BUFFERED = "atomic_buffered"  # Mode B: Fully buffered, validated, and synthetically replayed


class MidStreamFailurePolicy(str, Enum):
    """Policy when provider drops connection or errors midway through streaming."""
    ABORT = "abort"  # Terminate stream immediately with error event
    RESTART_FULL_RESPONSE = "restart_full_response"  # Restart turn on fallback provider
    DISABLE_FALLBACK_AFTER_FIRST_BYTE = "disable_fallback_after_first_byte"  # Prevent corrupting client token stream


@dataclass
class StreamingMetrics:
    """Telemetry captured for a streaming session."""
    mode: StreamingMode
    ttft_ms: float = 0.0
    total_duration_ms: float = 0.0
    bytes_streamed: int = 0
    chunks_emitted: int = 0
    fallback_occurred: bool = False


def synthesize_anthropic_sse(resp: NormalizedResponse, requested_model: str) -> Iterator[str]:
    """Generate clean synthetic Anthropic SSE events from a NormalizedResponse."""
    msg_id = resp.response_id if resp.response_id.startswith("msg_") else f"msg_{resp.response_id}"

    # 1. message_start
    yield (
        "event: message_start\n"
        + f"data: {json.dumps({'type': 'message_start', 'message': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'model': requested_model, 'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': resp.input_tokens, 'output_tokens': 1}}}, ensure_ascii=False)}\n\n"
    )

    block_idx = 0

    # 2. Thinking block if present
    if resp.reasoning_content:
        yield (
            "event: content_block_start\n"
            + f"data: {json.dumps({'type': 'content_block_start', 'index': block_idx, 'content_block': {'type': 'thinking', 'thinking': ''}}, ensure_ascii=False)}\n\n"
        )
        yield (
            "event: content_block_delta\n"
            + f"data: {json.dumps({'type': 'content_block_delta', 'index': block_idx, 'delta': {'type': 'thinking_delta', 'thinking': resp.reasoning_content}}, ensure_ascii=False)}\n\n"
        )
        yield (
            "event: content_block_stop\n"
            + f"data: {json.dumps({'type': 'content_block_stop', 'index': block_idx}, ensure_ascii=False)}\n\n"
        )
        block_idx += 1

    # 3. Text block if present
    if resp.content:
        yield (
            "event: content_block_start\n"
            + f"data: {json.dumps({'type': 'content_block_start', 'index': block_idx, 'content_block': {'type': 'text', 'text': ''}}, ensure_ascii=False)}\n\n"
        )
        yield (
            "event: content_block_delta\n"
            + f"data: {json.dumps({'type': 'content_block_delta', 'index': block_idx, 'delta': {'type': 'text_delta', 'text': resp.content}}, ensure_ascii=False)}\n\n"
        )
        yield (
            "event: content_block_stop\n"
            + f"data: {json.dumps({'type': 'content_block_stop', 'index': block_idx}, ensure_ascii=False)}\n\n"
        )
        block_idx += 1

    # 4. Tool calls if present
    for tc in resp.tool_calls:
        yield (
            "event: content_block_start\n"
            + f"data: {json.dumps({'type': 'content_block_start', 'index': block_idx, 'content_block': {'type': 'tool_use', 'id': tc.id, 'name': tc.name, 'input': {}}}, ensure_ascii=False)}\n\n"
        )
        json_str = tc.raw_arguments or json.dumps(tc.arguments, ensure_ascii=False)
        yield (
            "event: content_block_delta\n"
            + f"data: {json.dumps({'type': 'content_block_delta', 'index': block_idx, 'delta': {'type': 'input_json_delta', 'partial_json': json_str}}, ensure_ascii=False)}\n\n"
        )
        yield (
            "event: content_block_stop\n"
            + f"data: {json.dumps({'type': 'content_block_stop', 'index': block_idx}, ensure_ascii=False)}\n\n"
        )
        block_idx += 1

    # 5. message_delta & message_stop
    stop_map = {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
    }
    stop_reason = stop_map.get(resp.finish_reason, "end_turn")
    yield (
        "event: message_delta\n"
        + f"data: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None}, 'usage': {'output_tokens': resp.output_tokens}}, ensure_ascii=False)}\n\n"
    )
    yield "event: message_stop\n" + f"data: {json.dumps({'type': 'message_stop'}, ensure_ascii=False)}\n\n"


def synthesize_openai_sse(resp: NormalizedResponse, requested_model: str) -> Iterator[str]:
    """Generate standard OpenAI SSE chunks from a NormalizedResponse."""
    chat_id = resp.response_id if resp.response_id.startswith("chatcmpl-") else f"chatcmpl-{resp.response_id}"

    delta: Dict[str, Any] = {"role": "assistant"}
    if resp.content:
        delta["content"] = resp.content
    if resp.reasoning_content:
        delta["reasoning_content"] = resp.reasoning_content
    if resp.tool_calls:
        delta["tool_calls"] = [
            {
                "index": idx,
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.raw_arguments or json.dumps(tc.arguments)},
            }
            for idx, tc in enumerate(resp.tool_calls)
        ]

    finish = "tool_calls" if resp.tool_calls else resp.finish_reason

    chunk = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": requested_model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish,
            }
        ],
    }
    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"
