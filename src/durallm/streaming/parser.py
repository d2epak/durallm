"""Robust SSE Frame Parser and Wire Stream Normalizer (Frontier 1).

Guarantees live wire conformance against real-world provider quirks:
1. DeepSeek 2-byte chunk splits across SSE frames.
2. Anthropic extended thinking (thinking_delta, signature_delta).
3. OpenAI fragmented tool call arguments across chunk boundaries.
4. Partial multibyte UTF-8 byte boundary preservation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger("durallm.streaming.parser")


@dataclass
class ParsedSSEEvent:
    """A fully reassembled SSE message."""
    event: str = "message"
    data: str = ""
    json_data: Optional[Dict[str, Any]] = None
    is_done: bool = False


class SSEStreamParser:
    """Buffer and parse arbitrary byte chunks into discrete, validated SSE events."""

    def __init__(self) -> None:
        self._buffer: bytes = b""
        self._event_type: str = "message"
        self._data_lines: List[str] = []

    def feed(self, chunk: bytes) -> Iterator[ParsedSSEEvent]:
        """Feed a raw byte chunk from the network and yield any completed SSE events."""
        self._buffer += chunk

        # Process lines separated by \n or \r\n
        while b"\n" in self._buffer:
            line_bytes, self._buffer = self._buffer.split(b"\n", 1)
            # Strip trailing carriage return if present
            if line_bytes.endswith(b"\r"):
                line_bytes = line_bytes[:-1]

            # Blank line indicates end of an SSE event
            if not line_bytes:
                if self._data_lines:
                    data_str = "\n".join(self._data_lines)
                    is_done = (data_str.strip() == "[DONE]")
                    parsed_json: Optional[Dict[str, Any]] = None
                    if not is_done and data_str:
                        try:
                            parsed_json = json.loads(data_str)
                        except Exception:
                            parsed_json = None

                    yield ParsedSSEEvent(
                        event=self._event_type,
                        data=data_str,
                        json_data=parsed_json,
                        is_done=is_done,
                    )
                    self._event_type = "message"
                    self._data_lines = []
                continue

            try:
                line_str = line_bytes.decode("utf-8")
            except UnicodeDecodeError:
                # If byte split occurred inside a multi-byte sequence, put line back and wait for more bytes
                self._buffer = line_bytes + b"\n" + self._buffer
                break

            if line_str.startswith(":"):
                # SSE comment / heartbeat line, ignore
                continue
            if line_str.startswith("event:"):
                self._event_type = line_str[6:].strip()
            elif line_str.startswith("data:"):
                self._data_lines.append(line_str[5:].strip())

    def flush(self) -> Iterator[ParsedSSEEvent]:
        """Flush any trailing data in the buffer upon stream close."""
        if self._data_lines:
            data_str = "\n".join(self._data_lines)
            is_done = (data_str.strip() == "[DONE]")
            parsed_json: Optional[Dict[str, Any]] = None
            if not is_done and data_str:
                try:
                    parsed_json = json.loads(data_str)
                except Exception:
                    pass
            yield ParsedSSEEvent(
                event=self._event_type,
                data=data_str,
                json_data=parsed_json,
                is_done=is_done,
            )
            self._data_lines = []
        self._buffer = b""


class WireStreamAssembler:
    """Stateful accumulator that reassembles text, thinking, and tool arguments from SSE events."""

    def __init__(self) -> None:
        self.text_content: str = ""
        self.reasoning_content: str = ""
        self.reasoning_signature: Optional[str] = None
        self.tool_calls: Dict[int, Dict[str, Any]] = {}  # index -> {id, name, arguments_parts}
        self.finish_reason: Optional[str] = None

    def process_event(self, event: ParsedSSEEvent) -> None:
        """Accumulate content from a parsed SSE event (OpenAI or Anthropic)."""
        if event.is_done or not event.json_data:
            return

        payload = event.json_data

        # 1. Anthropic Event Handling
        etype = event.event or payload.get("type", "")
        if etype == "content_block_delta":
            delta = payload.get("delta", {})
            dtype = delta.get("type")
            if dtype == "text_delta":
                self.text_content += delta.get("text", "")
            elif dtype == "thinking_delta":
                self.reasoning_content += delta.get("thinking", "")
            elif dtype == "signature_delta":
                self.reasoning_signature = delta.get("signature")
            elif dtype == "input_json_delta":
                idx = payload.get("index", 0)
                if idx not in self.tool_calls:
                    self.tool_calls[idx] = {"id": "", "name": "", "args": ""}
                self.tool_calls[idx]["args"] += delta.get("partial_json", "")

        elif etype == "content_block_start":
            cb = payload.get("content_block", {})
            if cb.get("type") == "tool_use":
                idx = payload.get("index", 0)
                self.tool_calls[idx] = {
                    "id": cb.get("id", ""),
                    "name": cb.get("name", ""),
                    "args": "",
                }

        # 2. OpenAI / DeepSeek Event Handling
        choices = payload.get("choices", [])
        if choices:
            c0 = choices[0]
            if c0.get("finish_reason"):
                self.finish_reason = c0["finish_reason"]
            delta = c0.get("delta", {})
            if delta.get("content"):
                self.text_content += delta["content"]
            if delta.get("reasoning_content"):
                self.reasoning_content += delta["reasoning_content"]

            tc_deltas = delta.get("tool_calls", [])
            for tc in tc_deltas:
                idx = tc.get("index", 0)
                if idx not in self.tool_calls:
                    self.tool_calls[idx] = {"id": "", "name": "", "args": ""}
                if tc.get("id"):
                    self.tool_calls[idx]["id"] = tc["id"]
                fn = tc.get("function", {})
                if fn.get("name"):
                    self.tool_calls[idx]["name"] = fn["name"]
                if fn.get("arguments"):
                    self.tool_calls[idx]["args"] += fn["arguments"]

    def assembled_tool_calls(self) -> List[Dict[str, Any]]:
        """Return list of reassembled tool call dicts with parsed arguments."""
        result = []
        for idx in sorted(self.tool_calls.keys()):
            tc = self.tool_calls[idx]
            raw_args = tc.get("args", "")
            try:
                parsed_args = json.loads(raw_args) if raw_args else {}
            except Exception:
                parsed_args = {"_raw": raw_args}
            result.append({
                "id": tc.get("id", ""),
                "name": tc.get("name", ""),
                "arguments": parsed_args,
            })
        return result
