"""OpenAI Chat Completions Protocol Adapter for Normalized IR."""

from __future__ import annotations

import json
import time
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


def openai_request_to_ir(openai_body: Dict[str, Any]) -> NormalizedRequest:
    """Convert OpenAI /v1/chat/completions payload into NormalizedRequest IR."""
    req_id = f"req_{uuid.uuid4().hex[:12]}"
    model = openai_body.get("model", "default")

    system_instruction: Optional[str] = None
    messages: List[NormalizedMessage] = []

    for msg in openai_body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "") or ""
        reasoning = msg.get("reasoning_content") or msg.get("thinking")

        if role == "system" and not system_instruction:
            system_instruction = str(content)

        tool_calls: List[NormalizedToolCall] = []
        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            raw_args = fn.get("arguments", "{}")
            if isinstance(raw_args, dict):
                args = raw_args
                raw_str = json.dumps(raw_args, ensure_ascii=False)
            else:
                raw_str = str(raw_args)
                try:
                    args = json.loads(raw_str)
                except Exception:
                    args = {}
            tool_calls.append(
                NormalizedToolCall(
                    id=tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                    name=fn.get("name", ""),
                    arguments=args,
                    raw_arguments=raw_str,
                )
            )

        tool_results: List[NormalizedToolResult] = []
        if role == "tool":
            tool_results.append(
                NormalizedToolResult(
                    tool_call_id=msg.get("tool_call_id", ""),
                    tool_name=msg.get("name"),
                    content=str(content),
                )
            )

        messages.append(
            NormalizedMessage(
                role=role,
                content=str(content) if content else "",
                reasoning_content=str(reasoning) if reasoning else None,
                tool_calls=tool_calls,
                tool_results=tool_results,
                name=msg.get("name"),
            )
        )

    # Tools
    tools: List[NormalizedToolDefinition] = []
    for t in openai_body.get("tools", []):
        if t.get("type") == "function" and "function" in t:
            fn = t["function"]
            tools.append(
                NormalizedToolDefinition(
                    name=fn.get("name", ""),
                    description=fn.get("description", ""),
                    parameters=fn.get("parameters", {"type": "object", "properties": {}}),
                )
            )

    return NormalizedRequest(
        request_id=req_id,
        model=model,
        messages=messages,
        system_instruction=system_instruction,
        tools=tools,
        tool_choice=openai_body.get("tool_choice"),
        max_output_tokens=openai_body.get("max_tokens"),
        temperature=openai_body.get("temperature"),
        stream=bool(openai_body.get("stream", False)),
    )


def tool_choice_to_openai(tool_choice: Any) -> Optional[Any]:
    """Accept OpenAI- or Anthropic-style tool_choice and return the OpenAI form."""
    if isinstance(tool_choice, str):
        return tool_choice if tool_choice in ("auto", "required", "none") else None
    if isinstance(tool_choice, dict):
        kind = tool_choice.get("type")
        if kind == "function":
            return tool_choice
        if kind in ("auto", "none"):
            return kind
        if kind == "any":
            return "required"
        if kind == "tool" and tool_choice.get("name"):
            return {"type": "function", "function": {"name": tool_choice["name"]}}
    return None


def ir_to_openai_request(req: NormalizedRequest, target_model: str) -> Dict[str, Any]:
    """Convert NormalizedRequest IR into native OpenAI /v1/chat/completions payload."""
    payload_messages: List[Dict[str, Any]] = []

    # System prompt
    has_system = any(m.role == "system" for m in req.messages)
    if req.system_instruction and not has_system:
        payload_messages.append({"role": "system", "content": req.system_instruction})

    # Tool ID to Name index for tool turns
    tool_id_to_name: Dict[str, str] = {}
    for m in req.messages:
        for tc in m.tool_calls:
            if tc.id and tc.name:
                tool_id_to_name[tc.id] = tc.name

    for m in req.messages:
        if m.role == "system":
            payload_messages.append({"role": "system", "content": m.content})
        elif m.role == "user":
            if m.content:
                payload_messages.append({"role": "user", "content": m.content})
            for tr in m.tool_results:
                tool_name = tr.tool_name or tool_id_to_name.get(tr.tool_call_id, "tool")
                payload_messages.append({
                    "role": "tool",
                    "tool_call_id": tr.tool_call_id,
                    "name": tool_name,
                    "content": tr.content,
                })
        elif m.role == "assistant":
            asst_msg: Dict[str, Any] = {"role": "assistant"}
            if m.content:
                asst_msg["content"] = m.content
            else:
                asst_msg["content"] = None
            if m.reasoning_content:
                asst_msg["reasoning_content"] = m.reasoning_content
            if m.tool_calls:
                asst_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": tc.raw_arguments or json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in m.tool_calls
                ]
            payload_messages.append(asst_msg)
        elif m.role == "tool":
            for tr in m.tool_results:
                tool_name = tr.tool_name or tool_id_to_name.get(tr.tool_call_id, "tool")
                payload_messages.append({
                    "role": "tool",
                    "tool_call_id": tr.tool_call_id,
                    "name": tool_name,
                    "content": tr.content,
                })
            if not m.tool_results:
                payload_messages.append({
                    "role": "tool",
                    "content": m.content,
                })

    payload: Dict[str, Any] = {
        "model": target_model,
        "messages": payload_messages,
    }

    if req.max_output_tokens:
        payload["max_tokens"] = req.max_output_tokens
    if req.temperature is not None:
        payload["temperature"] = req.temperature
    if req.stream:
        payload["stream"] = True

    if req.tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in req.tools
        ]
        choice = tool_choice_to_openai(req.tool_choice)
        if choice is not None:
            payload["tool_choice"] = choice

    return payload


def openai_response_to_ir(openai_resp: Dict[str, Any]) -> NormalizedResponse:
    """Convert OpenAI /v1/chat/completions response into NormalizedResponse IR."""
    choices = openai_resp.get("choices", [])
    first_choice = choices[0] if choices else {}
    msg = first_choice.get("message", {})

    content = msg.get("content")
    reasoning = msg.get("reasoning_content") or msg.get("thinking")

    tool_calls: List[NormalizedToolCall] = []
    for tc in msg.get("tool_calls", []):
        fn = tc.get("function", {})
        raw_args = fn.get("arguments", "{}")
        if isinstance(raw_args, dict):
            args = raw_args
            raw_str = json.dumps(raw_args, ensure_ascii=False)
        else:
            raw_str = str(raw_args)
            try:
                args = json.loads(raw_str)
            except Exception:
                args = {}
        tool_calls.append(
            NormalizedToolCall(
                id=tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                name=fn.get("name", ""),
                arguments=args,
                raw_arguments=raw_str,
            )
        )

    finish_reason = first_choice.get("finish_reason", "stop")
    usage = openai_resp.get("usage", {})

    return NormalizedResponse(
        response_id=openai_resp.get("id", f"resp_{uuid.uuid4().hex[:12]}"),
        model=openai_resp.get("model", "default"),
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        input_tokens=usage.get("prompt_tokens", 0),
        output_tokens=usage.get("completion_tokens", 0),
        raw_response=openai_resp,
    )


def ir_to_openai_response(resp: NormalizedResponse, requested_model: str) -> Dict[str, Any]:
    """Convert NormalizedResponse IR into native OpenAI ChatCompletion response format."""
    message: Dict[str, Any] = {
        "role": "assistant",
        "content": resp.content,
    }
    if resp.reasoning_content:
        message["reasoning_content"] = resp.reasoning_content

    if resp.tool_calls:
        message["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": tc.raw_arguments or json.dumps(tc.arguments, ensure_ascii=False),
                },
            }
            for tc in resp.tool_calls
        ]

    finish_reason = "tool_calls" if resp.tool_calls else resp.finish_reason

    return {
        "id": resp.response_id if resp.response_id.startswith("chatcmpl-") else f"chatcmpl-{resp.response_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": requested_model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": resp.input_tokens,
            "completion_tokens": resp.output_tokens,
            "total_tokens": resp.input_tokens + resp.output_tokens,
        },
    }
