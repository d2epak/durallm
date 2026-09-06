"""Anthropic Protocol Adapter for Normalized IR."""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from llm_circuit_breaker.protocol.ir import (
    NormalizedMessage,
    NormalizedRequest,
    NormalizedResponse,
    NormalizedToolCall,
    NormalizedToolDefinition,
    NormalizedToolResult,
)


def anthropic_request_to_ir(anthropic_body: Dict[str, Any]) -> NormalizedRequest:
    """Convert Anthropic /v1/messages body into NormalizedRequest IR."""
    req_id = f"req_{uuid.uuid4().hex[:12]}"
    model = anthropic_body.get("model", "auto-coding-agent")

    # 1. System Prompt
    system_text: Optional[str] = None
    raw_system = anthropic_body.get("system")
    if raw_system:
        if isinstance(raw_system, str):
            system_text = raw_system
        elif isinstance(raw_system, list):
            parts = [b.get("text", "") for b in raw_system if isinstance(b, dict) and b.get("type") == "text"]
            if parts:
                system_text = "\n".join(parts)

    # 2. Tool Definitions
    tools: List[NormalizedToolDefinition] = []
    for t in anthropic_body.get("tools", []):
        if isinstance(t, dict):
            tools.append(
                NormalizedToolDefinition(
                    name=t.get("name", ""),
                    description=t.get("description", ""),
                    parameters=t.get("input_schema", {"type": "object", "properties": {}}),
                )
            )

    # 3. Messages
    normalized_messages: List[NormalizedMessage] = []
    for msg in anthropic_body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content")

        if isinstance(content, str):
            normalized_messages.append(NormalizedMessage(role=role, content=content))
            continue

        if not isinstance(content, list):
            normalized_messages.append(NormalizedMessage(role=role, content=str(content or "")))
            continue

        # Content blocks
        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        signatures: List[str] = []
        tool_calls: List[NormalizedToolCall] = []
        tool_results: List[NormalizedToolResult] = []

        for b in content:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "text":
                text_parts.append(b.get("text", ""))
            elif btype == "thinking":
                reasoning_parts.append(b.get("thinking", ""))
                if b.get("signature"):
                    signatures.append(b["signature"])
            elif btype == "tool_use":
                tid = b.get("id") or f"toolu_{uuid.uuid4().hex[:8]}"
                tname = b.get("name", "")
                tinput = b.get("input", {})
                tool_calls.append(
                    NormalizedToolCall(
                        id=tid,
                        name=tname,
                        arguments=tinput if isinstance(tinput, dict) else {},
                        raw_arguments=json.dumps(tinput, ensure_ascii=False) if isinstance(tinput, dict) else str(tinput),
                    )
                )
            elif btype == "tool_result":
                tr_id = b.get("tool_use_id", "")
                tr_content = b.get("content", "")
                if isinstance(tr_content, list):
                    tr_content = "\n".join([x.get("text", "") for x in tr_content if isinstance(x, dict)])
                tool_results.append(
                    NormalizedToolResult(
                        tool_call_id=tr_id,
                        content=str(tr_content),
                        is_error=bool(b.get("is_error", False)),
                    )
                )

        normalized_messages.append(
            NormalizedMessage(
                role=role,
                content="\n".join(text_parts) if text_parts else "",
                reasoning_content="\n".join(reasoning_parts) if reasoning_parts else None,
                # A signature covers one block's exact text, so it survives only when there is one block.
                reasoning_signature=signatures[0] if len(reasoning_parts) == 1 and signatures else None,
                tool_calls=tool_calls,
                tool_results=tool_results,
            )
        )

    return NormalizedRequest(
        request_id=req_id,
        model=model,
        messages=normalized_messages,
        system_instruction=system_text,
        tools=tools,
        tool_choice=anthropic_body.get("tool_choice"),
        max_output_tokens=anthropic_body.get("max_tokens"),
        temperature=anthropic_body.get("temperature"),
        stream=bool(anthropic_body.get("stream", False)),
    )


def tool_choice_to_anthropic(tool_choice: Any) -> Optional[Dict[str, Any]]:
    """Accept OpenAI- or Anthropic-style tool_choice and return the Anthropic form."""
    if isinstance(tool_choice, str):
        return {"auto": {"type": "auto"}, "required": {"type": "any"}, "none": {"type": "none"}}.get(tool_choice)
    if isinstance(tool_choice, dict):
        kind = tool_choice.get("type")
        if kind in ("auto", "any", "tool", "none"):
            return tool_choice
        if kind == "function":
            name = (tool_choice.get("function") or {}).get("name")
            return {"type": "tool", "name": name} if name else None
    return None


def ir_to_anthropic_request(req: NormalizedRequest, target_model: str) -> Dict[str, Any]:
    """Convert NormalizedRequest IR into native Anthropic /v1/messages payload."""
    payload: Dict[str, Any] = {
        "model": target_model,
        "messages": [],
    }

    if req.system_instruction:
        payload["system"] = req.system_instruction

    if req.max_output_tokens:
        payload["max_tokens"] = req.max_output_tokens
    if req.temperature is not None:
        payload["temperature"] = req.temperature
    if req.stream:
        payload["stream"] = True

    # Tools
    if req.tools:
        payload["tools"] = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
            }
            for t in req.tools
        ]
        choice = tool_choice_to_anthropic(req.tool_choice)
        if choice is not None:
            payload["tool_choice"] = choice

    # Messages
    for m in req.messages:
        if m.role == "system":
            # If not set in system field, prepend to system or keep
            if "system" not in payload:
                payload["system"] = m.content
            continue

        content_blocks: List[Dict[str, Any]] = []
        if m.reasoning_content and m.reasoning_signature:
            # Anthropic rejects thinking blocks without their signature; unsigned reasoning
            # (e.g. from an OpenAI-style upstream) is dropped rather than sent back.
            content_blocks.append({"type": "thinking", "thinking": m.reasoning_content, "signature": m.reasoning_signature})
        if m.content:
            content_blocks.append({"type": "text", "text": m.content})
        for tc in m.tool_calls:
            content_blocks.append({
                "type": "tool_use",
                "id": tc.id,
                "name": tc.name,
                "input": tc.arguments,
            })
        for tr in m.tool_results:
            content_blocks.append({
                "type": "tool_result",
                "tool_use_id": tr.tool_call_id,
                "content": tr.content,
                "is_error": tr.is_error,
            })

        payload["messages"].append({
            "role": m.role,
            "content": content_blocks if content_blocks else m.content,
        })

    return payload


def ir_to_anthropic_response(resp: NormalizedResponse, requested_model: str) -> Dict[str, Any]:
    """Convert NormalizedResponse IR into native Anthropic /v1/messages response format."""
    blocks: List[Dict[str, Any]] = []

    if resp.reasoning_content and resp.reasoning_signature:
        blocks.append({"type": "thinking", "thinking": resp.reasoning_content, "signature": resp.reasoning_signature})

    if resp.content:
        blocks.append({"type": "text", "text": resp.content})

    for tc in resp.tool_calls:
        blocks.append({
            "type": "tool_use",
            "id": tc.id or f"toolu_{uuid.uuid4().hex[:8]}",
            "name": tc.name,
            "input": tc.arguments,
        })

    # Map finish reason
    stop_map = {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
    }
    stop_reason = stop_map.get(resp.finish_reason, "end_turn")

    return {
        "id": resp.response_id if resp.response_id.startswith("msg_") else f"msg_{resp.response_id}",
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": resp.input_tokens,
            "output_tokens": resp.output_tokens,
        },
    }
