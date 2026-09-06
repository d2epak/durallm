"""Google Gemini REST Protocol Adapter for Normalized IR.

Handles Protobuf schema sanitization (`clean_gemini_schema`) and converts
between Normalized IR and Google AI Studio `/v1beta/models/...:generateContent`.
"""

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
)
from llm_circuit_breaker.translators import clean_gemini_schema


def tool_choice_to_gemini(tool_choice: Any) -> Optional[Dict[str, Any]]:
    """Map OpenAI- or Anthropic-style tool_choice onto Gemini functionCallingConfig."""
    mode: Optional[str] = None
    names: List[str] = []
    if isinstance(tool_choice, str):
        mode = {"auto": "AUTO", "required": "ANY", "none": "NONE"}.get(tool_choice)
    elif isinstance(tool_choice, dict):
        kind = tool_choice.get("type")
        if kind in ("auto", "none"):
            mode = kind.upper()
        elif kind == "any":
            mode = "ANY"
        elif kind == "tool" and tool_choice.get("name"):
            mode, names = "ANY", [tool_choice["name"]]
        elif kind == "function" and (tool_choice.get("function") or {}).get("name"):
            mode, names = "ANY", [tool_choice["function"]["name"]]
    if mode is None:
        return None
    config: Dict[str, Any] = {"mode": mode}
    if names:
        config["allowedFunctionNames"] = names
    return {"functionCallingConfig": config}


def ir_to_gemini_request(req: NormalizedRequest, target_model: str) -> Dict[str, Any]:
    """Convert NormalizedRequest IR into Gemini generateContent payload with sanitized schema."""
    contents: List[Dict[str, Any]] = []
    system_instruction: Optional[Dict[str, Any]] = None

    sys_text = req.get_effective_system_instruction()
    if sys_text:
        system_instruction = {"parts": [{"text": sys_text}]}

    for m in req.messages:
        if m.role == "system":
            continue

        if m.role == "user":
            user_parts: List[Dict[str, Any]] = []
            if m.content:
                user_parts.append({"text": m.content})
            for tr in m.tool_results:
                user_parts.append({
                    "functionResponse": {
                        "name": tr.tool_name or "tool",
                        "response": {"output": tr.content},
                    }
                })
            contents.append({"role": "user", "parts": user_parts or [{"text": ""}]})

        elif m.role == "assistant":
            parts: List[Dict[str, Any]] = []
            if m.content:
                parts.append({"text": m.content})
            for tc in m.tool_calls:
                parts.append({
                    "functionCall": {
                        "name": tc.name,
                        "args": tc.arguments,
                    }
                })
            contents.append({"role": "model", "parts": parts or [{"text": ""}]})

        elif m.role == "tool":
            for tr in m.tool_results:
                contents.append({
                    "role": "function",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": tr.tool_name or "tool",
                                "response": {"output": tr.content},
                            }
                        }
                    ],
                })
            if not m.tool_results and m.content:
                contents.append({
                    "role": "function",
                    "parts": [{"functionResponse": {"name": m.name or "tool", "response": {"output": m.content}}}],
                })

    gemini_req: Dict[str, Any] = {"contents": contents}
    if system_instruction:
        gemini_req["systemInstruction"] = system_instruction

    # Tools
    if req.tools:
        declarations = []
        for t in req.tools:
            cleaned_params = clean_gemini_schema(t.parameters)
            declarations.append({
                "name": t.name,
                "description": t.description,
                "parameters": cleaned_params,
            })
        gemini_req["tools"] = [{"functionDeclarations": declarations}]
        tool_config = tool_choice_to_gemini(req.tool_choice)
        if tool_config is not None:
            gemini_req["toolConfig"] = tool_config

    # Generation Config
    gen_config: Dict[str, Any] = {}
    if req.max_output_tokens:
        gen_config["maxOutputTokens"] = req.max_output_tokens
    if req.temperature is not None:
        gen_config["temperature"] = req.temperature
    if gen_config:
        gemini_req["generationConfig"] = gen_config

    return gemini_req


def gemini_response_to_ir(gemini_resp: Dict[str, Any], model_name: str) -> NormalizedResponse:
    """Convert Gemini generateContent response into NormalizedResponse IR."""
    candidates = gemini_resp.get("candidates", [])
    if not candidates:
        return NormalizedResponse(
            response_id=f"gemini_{uuid.uuid4().hex[:8]}",
            model=model_name,
            content="",
            finish_reason="stop",
            raw_response=gemini_resp,
        )

    first = candidates[0]
    content_obj = first.get("content", {})
    parts = content_obj.get("parts", [])

    text_pieces: List[str] = []
    tool_calls: List[NormalizedToolCall] = []

    for p in parts:
        if "text" in p:
            text_pieces.append(p["text"])
        elif "functionCall" in p:
            fc = p["functionCall"]
            args = fc.get("args", {})
            tool_calls.append(
                NormalizedToolCall(
                    id=f"call_{uuid.uuid4().hex[:8]}",
                    name=fc.get("name", ""),
                    arguments=args if isinstance(args, dict) else {},
                    raw_arguments=json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args),
                )
            )

    usage = gemini_resp.get("usageMetadata", {})
    finish_reason = "tool_calls" if tool_calls else "stop"

    return NormalizedResponse(
        response_id=f"gemini_{uuid.uuid4().hex[:8]}",
        model=model_name,
        content="".join(text_pieces) if text_pieces else None,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        input_tokens=usage.get("promptTokenCount", 0),
        output_tokens=usage.get("candidatesTokenCount", 0),
        raw_response=gemini_resp,
    )
