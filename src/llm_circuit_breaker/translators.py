"""Bidirectional Protocol Translators & Adapters.

Enables Anthropic Claude Code clients to communicate seamlessly with OpenAI,
Google Gemini REST, and open-weights models without protocol mismatches.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from llm_circuit_breaker.protocol.gemini import clean_gemini_schema  # noqa: F401  (re-exported)


def repair_json_string(raw: str) -> Optional[str]:
    """Repair minor JSON formatting anomalies from open-weights models.

    Returns a string that parses as a JSON object, or ``None`` when the input
    cannot be repaired. It never invents arguments: an unparseable payload
    must not be turned into a ``{"command": ...}`` invocation.
    """
    if not raw or not raw.strip():
        return "{}"
    cleaned = raw.strip()
    # Remove markdown backticks if wrapped in ```json ... ```
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    # Remove trailing commas before closing braces/brackets
    cleaned = re.sub(r",\s*([\]}])", r"\1", cleaned)
    try:
        parsed = json.loads(cleaned)
    except Exception:
        return None
    return cleaned if isinstance(parsed, dict) else None


def convert_openai_to_gemini_payload(openai_req: Dict[str, Any]) -> Dict[str, Any]:
    """Convert standard OpenAI chat request to Gemini generateContent format."""
    contents: List[Dict[str, Any]] = []
    system_instruction: Optional[Dict[str, Any]] = None

    for msg in openai_req.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "") or ""

        if role == "system":
            system_instruction = {"parts": [{"text": str(content)}]}
        elif role == "user":
            contents.append({"role": "user", "parts": [{"text": str(content)}]})
        elif role == "assistant":
            parts = []
            if content:
                parts.append({"text": str(content)})
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                args = fn.get("arguments", "{}")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                parts.append({"functionCall": {"name": fn.get("name"), "args": args}})
            contents.append({"role": "model", "parts": parts or [{"text": ""}]})
        elif role == "tool":
            fn_name = msg.get("name") or "bash"
            contents.append({
                "role": "function",
                "parts": [{
                    "functionResponse": {
                        "name": fn_name,
                        "response": {"output": str(content)}
                    }
                }]
            })

    gemini_req: Dict[str, Any] = {"contents": contents}
    if system_instruction:
        gemini_req["systemInstruction"] = system_instruction

    # Tools
    openai_tools = openai_req.get("tools")
    if openai_tools:
        declarations = []
        for t in openai_tools:
            fn = t.get("function", {})
            raw_params = fn.get("parameters", {"type": "object", "properties": {}})
            cleaned_params = clean_gemini_schema(raw_params)
            declarations.append({
                "name": fn.get("name"),
                "description": fn.get("description", ""),
                "parameters": cleaned_params
            })
        gemini_req["tools"] = [{"functionDeclarations": declarations}]

    return gemini_req


def convert_gemini_to_openai_response(gemini_resp: Dict[str, Any], model_name: str) -> Dict[str, Any]:
    """Convert Gemini API response to standard OpenAI ChatCompletion structure."""
    candidates = gemini_resp.get("candidates", [])
    if not candidates:
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_name,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
        }

    first = candidates[0]
    content = first.get("content", {})
    parts = content.get("parts", [])
    text_pieces = []
    tool_calls = []

    for idx, p in enumerate(parts):
        if "text" in p:
            text_pieces.append(p["text"])
        elif "functionCall" in p:
            fc = p["functionCall"]
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": fc.get("name"),
                    "arguments": json.dumps(fc.get("args", {}))
                }
            })

    message: Dict[str, Any] = {"role": "assistant", "content": "".join(text_pieces) if text_pieces else None}
    if tool_calls:
        message["tool_calls"] = tool_calls

    finish_reason = "tool_calls" if tool_calls else "stop"
    usage_md = gemini_resp.get("usageMetadata", {})

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": usage_md.get("promptTokenCount", 0),
            "completion_tokens": usage_md.get("candidatesTokenCount", 0),
            "total_tokens": usage_md.get("totalTokenCount", 0)
        }
    }


def anthropic_to_openai_request(anthropic_req: Dict[str, Any], model_name: str) -> Dict[str, Any]:
    """Convert Anthropic /v1/messages body to standard OpenAI /v1/chat/completions."""
    messages: List[Dict[str, Any]] = []

    # 1. System Prompt
    system = anthropic_req.get("system")
    if system:
        if isinstance(system, str):
            messages.append({"role": "system", "content": system})
        elif isinstance(system, list):
            text_parts = [b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text"]
            if text_parts:
                messages.append({"role": "system", "content": "\n".join(text_parts)})

    # Build tool_id -> tool_name lookup map
    tool_id_to_name: Dict[str, str] = {}
    for msg in anthropic_req.get("messages", []):
        if msg.get("role") == "assistant":
            c = msg.get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tid = b.get("id")
                        tname = b.get("name")
                        if tid and tname:
                            tool_id_to_name[tid] = tname

    # 2. Conversation Messages
    for msg in anthropic_req.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content")

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            messages.append({"role": role, "content": str(content or "")})
            continue

        if role == "assistant":
            text_parts = []
            tool_calls = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type")
                if btype == "text":
                    text_parts.append(b.get("text", ""))
                elif btype == "tool_use":
                    tool_calls.append({
                        "id": b.get("id") or f"toolu_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {
                            "name": b.get("name"),
                            "arguments": json.dumps(b.get("input", {}), ensure_ascii=False)
                        }
                    })
            asst_msg: Dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts) if text_parts else None}
            if tool_calls:
                asst_msg["tool_calls"] = tool_calls
            messages.append(asst_msg)

        elif role == "user":
            user_text_parts = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type")
                if btype == "tool_result":
                    # Emit any prior user text before tool result
                    if user_text_parts:
                        messages.append({"role": "user", "content": "\n".join(user_text_parts)})
                        user_text_parts.clear()

                    tool_use_id = b.get("tool_use_id") or "call_unknown"
                    tool_name = tool_id_to_name.get(tool_use_id, "bash")
                    res_content = b.get("content", "")
                    if isinstance(res_content, list):
                        res_content = "\n".join([x.get("text", "") for x in res_content if isinstance(x, dict)])
                    messages.append({
                        "role": "tool",
                        "name": tool_name,
                        "tool_call_id": tool_use_id,
                        "content": str(res_content)
                    })
                elif btype == "text":
                    user_text_parts.append(b.get("text", ""))

            if user_text_parts:
                messages.append({"role": "user", "content": "\n".join(user_text_parts)})

    openai_req: Dict[str, Any] = {
        "model": model_name,
        "messages": messages,
        "stream": False,
    }

    if "max_tokens" in anthropic_req:
        openai_req["max_tokens"] = anthropic_req["max_tokens"]
    if "temperature" in anthropic_req:
        openai_req["temperature"] = anthropic_req["temperature"]

    # 3. Tool Declarations
    tools = anthropic_req.get("tools")
    if tools and isinstance(tools, list):
        openai_tools = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}})
                }
            })
        if openai_tools:
            openai_req["tools"] = openai_tools

    return openai_req


def openai_to_anthropic_response(openai_resp: Dict[str, Any], requested_model: str) -> Dict[str, Any]:
    """Convert OpenAI ChatCompletion response to Anthropic /v1/messages format."""
    choices = openai_resp.get("choices", [])
    msg = choices[0].get("message", {}) if choices else {}

    content_blocks: List[Dict[str, Any]] = []

    # 1. Reasoning / Thinking
    reasoning = msg.get("reasoning_content") or msg.get("thinking")
    if reasoning and isinstance(reasoning, str):
        content_blocks.append({"type": "thinking", "thinking": reasoning})

    # 2. Main Content
    text_content = msg.get("content")
    if text_content and isinstance(text_content, str):
        content_blocks.append({"type": "text", "text": text_content})

    # 3. Tool Calls
    tool_calls = msg.get("tool_calls", [])
    for tc in tool_calls:
        fn = tc.get("function", {})
        raw_args = fn.get("arguments", "{}")
        repaired_args = repair_json_string(raw_args)
        if repaired_args is None:
            # Fail closed: surface the malformed arguments as text instead of
            # fabricating a tool invocation the model never made.
            content_blocks.append({
                "type": "text",
                "text": f"[unparseable tool_call arguments for {fn.get('name')!r}: {raw_args}]",
            })
            continue

        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:8]}",
            "name": fn.get("name"),
            "input": json.loads(repaired_args)
        })

    finish_reason = choices[0].get("finish_reason", "stop") if choices else "stop"
    stop_reason_map = {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
    }
    anthropic_stop_reason = stop_reason_map.get(finish_reason, "end_turn")
    if anthropic_stop_reason == "tool_use" and not any(b["type"] == "tool_use" for b in content_blocks):
        anthropic_stop_reason = "end_turn"

    usage = openai_resp.get("usage", {})
    return {
        "id": openai_resp.get("id") or f"msg_{uuid.uuid4().hex[:12]}",
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": content_blocks,
        "stop_reason": anthropic_stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0)
        }
    }
